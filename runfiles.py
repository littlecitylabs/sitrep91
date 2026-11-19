"""
Where the toolkit keeps its files: the portable folder layout, settings.ini,
and the one-folder-per-slot-per-run naming.

Pure file and path logic - no BLE, no Qt - so any reader can use it without
pulling in bleak, and so all
of it can be tested offline.

THE PORTABLE LAYOUT
-------------------
    SITREP91\\
      SITREP91.bat
      python\\        <- the bundled interpreter; replaced on update
      app\\           <- the code; replaced on update
      data\\          <- everything the user keeps; never replaced
        runs\\        <- one folder per slot per run
        settings.ini

HOME_DIR is the folder that holds runs\\ and settings.ini. When the
code sits in a folder named `app` (the released layout) it is `data\\` beside
`app\\`, so "keep data\\, replace the rest" is the whole update instruction;
otherwise (a development checkout, where the code is the repo root) it is the
code's own folder, so a checkout keeps working exactly as before.
Everything is resolved from here, never from the working directory: a shortcut
with a different "Start in" must not be able to lose any of it.

THE RUN LAYOUT
--------------
    runs\\2026-09-22_1432_S2_TEST-02_discharge\\
        2026-09-22_1432_S2_TEST-02_discharge.csv
        meta.json

`<date>_<time>_S<n>_<cell>_<action>`: timestamp first so name order is time
order (local 24 h, minute resolution, no colons); the cell segment is left out
when there is no cell; the action is one word from ACTIONS. The CSV is named
after its folder so it describes itself when copied out on its own. Runs
recorded by older versions hold `samples.csv` instead - find_run_csv reads both.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

# The app's version: the title bar, and every run's meta.json, so a reader
# knows which code recorded a run. One place, so the CLI and GUI agree.
APP_VERSION = "1.0.0"

def home_dir_for(app_dir: Path) -> Path:
    """The home folder for code living in `app_dir` (see THE PORTABLE LAYOUT)."""
    if app_dir.name.lower() == "app":
        return app_dir.parent / "data"
    return app_dir


APP_DIR = Path(__file__).resolve().parent
HOME_DIR = home_dir_for(APP_DIR)

# Module-level so tests can point them elsewhere.
SETTINGS_PATH = HOME_DIR / "settings.ini"

# The action word in a run folder's name - the same word the card shows.
# `external` is a run this program watched but did not start (a front-panel
# run, or one another program started).
ACTIONS = ("charge", "discharge", "storage", "external")

# settings.ini: [logging] runs_dir = runs. Relative to HOME_DIR unless the
# user picked an absolute folder.
LOGGING_SECTION = "logging"
RUNS_DIR_KEY = "runs_dir"
DEFAULT_RUNS_DIR = "runs"

# Excel will not open a file whose FULL path is longer than this. The run name
# appears twice in it (folder and file), so the name is kept short and the
# logs folder is checked against it.
EXCEL_PATH_LIMIT = 218
# A cell id longer than this is cut in the folder name (never in meta.json).
CELL_SEGMENT_MAX = 16
# The CLI's --name, appended to its run folders; cut to this in the name.
LABEL_SEGMENT_MAX = 20
# Session logs (the CLI's running commentary) go here, inside the runs folder
# but outside the run-folder naming, so a run listing never mistakes one.
SESSION_LOG_DIR = "logs"

# Crash and diagnostics logs: <HOME_DIR>\logs - data\logs in the released layout.
# Fixed, never the configurable runs folder: a failure can come before that
# folder is known. SITREP91_LOG_DIR moves it (tests only).
CRASH_LOG_DIR = "logs"

_settings_lock = threading.Lock()


# --------------------------------------------------------------------------
# settings.ini
# --------------------------------------------------------------------------

def read_setting(section: str, key: str) -> str | None:
    """One value from settings.ini, or None. Never raises: a missing or damaged
    file simply means nothing is set."""
    cp = configparser.ConfigParser()
    try:
        with _settings_lock:
            cp.read(SETTINGS_PATH, encoding="utf-8")
        return cp.get(section, key, fallback=None)
    except (OSError, configparser.Error):
        return None


def write_setting(section: str, values: dict, log=None) -> bool:
    """Set `values` in one section of settings.ini, keeping everything else in
    the file. Locked, because the GUI thread (display settings) and the capture
    thread (firmware records) both write it. A failure is logged, never raised,
    and reported by the return value."""
    cp = configparser.ConfigParser()
    try:
        with _settings_lock:
            cp.read(SETTINGS_PATH, encoding="utf-8")
            if not cp.has_section(section):
                cp.add_section(section)
            for k, v in values.items():
                cp.set(section, k, str(v))
            # Atomic: write a temp file beside it, then replace. Rewriting in
            # place meant a crash (or a full disk) mid-write left a truncated
            # settings.ini - and the remembered firmware with it, after which
            # every start was refused until the version was read again.
            tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    cp.write(fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, SETTINGS_PATH)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        return True
    except (OSError, configparser.Error) as e:
        if log:
            log(f"  could not write {SETTINGS_PATH}: {e}")
        return False


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------

def default_runs_dir() -> Path:
    """The local default: runs\\ in the app folder. Also where a run is moved
    to when writing to a user-chosen folder fails mid-run."""
    return HOME_DIR / DEFAULT_RUNS_DIR


def configured_runs_dir() -> Path:
    """The logs folder from settings.ini, resolved against HOME_DIR when
    relative. Defaults to runs\\ beside the app."""
    value = (read_setting(LOGGING_SECTION, RUNS_DIR_KEY) or "").strip()
    if not value:
        return default_runs_dir()
    p = Path(value).expanduser()
    return p if p.is_absolute() else HOME_DIR / p


def runs_dir_setting_value(folder: str | Path) -> str:
    """What to store for `folder`: relative to HOME_DIR when it is inside it
    (so the whole folder stays portable), absolute otherwise."""
    folder = Path(folder)
    try:
        return str(folder.resolve().relative_to(HOME_DIR.resolve()))
    except ValueError:
        return str(folder.resolve())


def save_runs_dir(folder: str | Path, log=None) -> bool:
    return write_setting(LOGGING_SECTION,
                         {RUNS_DIR_KEY: runs_dir_setting_value(folder)}, log)


# What a first launch writes, so the file exists to look at and edit. These are
# the defaults the app already uses (gui_theme: [display] theme, system).
DEFAULT_SETTINGS = {"display": {"theme": "system"}}


def ensure_default_settings() -> None:
    """Write settings.ini with the defaults when there is no file yet. An
    existing file, even a damaged one, is never touched. It is not shipped in
    the zip because data\\ is the folder an update keeps, so a shipped copy
    would overwrite the user's saved settings. Best effort, like the folders."""
    if SETTINGS_PATH.exists():
        return
    for section, values in DEFAULT_SETTINGS.items():
        write_setting(section, values)


def ensure_home_folders() -> None:
    """Write the default settings.ini if there is none. (The name is from when
    this also made folders; runs\\ is made by the capture that first writes to
    it, and nothing else is created here.)"""
    ensure_default_settings()


# --------------------------------------------------------------------------
# startup guards
# --------------------------------------------------------------------------

def inside_temp_dir(folder: str | Path | None = None) -> bool:
    """
    True when `folder` (default HOME_DIR) looks like the inside of a zip that
    was double-clicked rather than extracted. Windows unpacks such a file to a
    throwaway folder under %TEMP% (e.g. ...\\Temp\\Temp1_MC5000.zip\\...), which
    is emptied later - runs recorded there would vanish.
    """
    p = Path(folder or HOME_DIR).resolve()
    if any(part.lower().endswith(".zip") for part in p.parts):
        return True
    try:
        temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return p == temp or temp in p.parents


INSIDE_ZIP_MESSAGE = (
    "This program is running from inside the zip file, so nothing it records "
    "would be kept.\n\nExtract the zip first (right-click it, Extract All), "
    "then run it from the extracted folder.")


def writable_problem(folder: str | Path) -> str | None:
    """Why runs cannot be written to `folder`, in plain words, or None if they
    can. Creates the folder if it is missing, then writes and deletes a small
    test file - the only honest test on Windows, where permission bits lie."""
    folder = Path(folder)
    probe = folder / f".write_test_{os.getpid()}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        probe.unlink()
    except OSError as e:
        return (f"Cannot write to {folder}: {e.strerror or e}. Recordings need "
                f"a folder this program can write to.")
    return None


def documents_fallback_dir() -> Path:
    """Where to offer recording when the app folder is not writable."""
    return Path.home() / "Documents" / "SITREP91" / DEFAULT_RUNS_DIR


def path_budget_problem(runs_dir: str | Path) -> str | None:
    """Whether a run file under `runs_dir` could exceed Excel's path limit.
    Checked against the LONGEST name this program can produce, so a folder that
    passes here always yields files Excel can open."""
    longest = run_folder_name(datetime(2026, 12, 31, 23, 59), 4,
                              "X" * CELL_SEGMENT_MAX, "discharge",
                              "L" * LABEL_SEGMENT_MAX) + "-99"
    full = Path(runs_dir) / longest / f"{longest}.csv"
    if len(str(full)) > EXCEL_PATH_LIMIT:
        return (f"Files in {runs_dir} could have paths up to {len(str(full))} "
                f"characters, and Excel cannot open files whose full path is "
                f"longer than {EXCEL_PATH_LIMIT}. Choose a folder with a "
                f"shorter path.")
    return None


# --------------------------------------------------------------------------
# run folders
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def cell_segment(cell_id: str | None,
                 limit: int = CELL_SEGMENT_MAX) -> str | None:
    """A cell id (or a label) made safe for a folder name: anything outside
    letters, digits, '.', '_' and '-' becomes '-', and it is cut to `limit`.
    Case is kept (history matching is case-insensitive). None when nothing is
    left."""
    if not cell_id:
        return None
    s = _UNSAFE.sub("-", cell_id.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    s = s[:limit].rstrip("-._")
    return s or None


def run_folder_name(started: datetime, slot: int, cell_id: str | None,
                    action: str, label: str | None = None) -> str:
    """`2026-09-22_1432_S2_TEST-02_discharge`; no cell -> no cell segment.

    `label` is the CLI's --name, appended LAST (`..._discharge_mytest`) so the
    timestamp still leads and name order stays time order, and research runs
    can be found by name. The GUI passes none: its
    session names are defaults, not something the user typed.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r} (expected one of "
                         f"{', '.join(ACTIONS)})")
    parts = [started.strftime("%Y-%m-%d_%H%M"), f"S{slot}"]
    cell = cell_segment(cell_id)
    if cell:
        parts.append(cell)
    parts.append(action)
    label = cell_segment(label, LABEL_SEGMENT_MAX)
    if label:
        parts.append(label)
    return "_".join(parts)


def unique_run_dir(parent: str | Path, name: str) -> Path:
    """`parent/name`, or `name-2`, `name-3` ... if that is taken (the same slot
    restarted within a minute). Not created here."""
    parent = Path(parent)
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}-{n}"
        n += 1
    return candidate


# Configure's cell-name list reads this many of the newest run folders and offers
# at most this many names: a long history (or a slow share) must not stall the
# dialog, and a name older than that is one typing still reaches.
PAST_CELL_SCAN = 300
PAST_CELL_LIMIT = 50


def past_cell_names(runs_dir: str | Path, *, scan: int = PAST_CELL_SCAN,
                    limit: int = PAST_CELL_LIMIT) -> list[str]:
    """
    The cell names earlier runs were recorded under, most recent first, read
    from each run folder's meta.json (`cell`, or the older `cells` mapping).

    No file of its own and no stored list: the runs ARE the history. Case is
    ignored when matching, and the most recent spelling wins. A folder with no
    or an unreadable meta.json (the logs folder, a half-copied run) is skipped,
    never an error, and a missing runs folder is an empty list. Folders are
    ordered by their meta.json's modified time, since run folders from older
    versions do not sort by name the way current ones do.
    """
    root = Path(runs_dir)
    found = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_dir():
                        continue
                    meta = Path(entry.path) / "meta.json"
                    found.append((meta.stat().st_mtime, meta))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    names: list[str] = []
    seen: set[str] = set()
    for _mtime, meta in found[:scan]:
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        candidates = [data.get("cell")]
        cells = data.get("cells")
        if isinstance(cells, dict):
            candidates += list(cells.values())
        for name in candidates:
            if not isinstance(name, str):
                continue
            name = name.strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
                if len(names) >= limit:
                    return names
    return names


def run_csv_name(run_dir: str | Path) -> str:
    """The CSV inside a run folder is named after the folder."""
    return f"{Path(run_dir).name}.csv"


def find_run_csv(run_dir: str | Path) -> Path:
    """
    The one .csv in a run folder. Prefers the one named after the folder, then
    the older `samples.csv`, then whatever single .csv is there (the
    folder may have been renamed). Raises FileNotFoundError when there is none
    and ValueError when there are several - never guesses between two.
    """
    d = Path(run_dir)
    for candidate in (d / run_csv_name(d), d / "samples.csv"):
        if candidate.is_file():
            return candidate
    csvs = sorted(p for p in d.glob("*.csv") if p.is_file())
    if len(csvs) == 1:
        return csvs[0]
    if not csvs:
        raise FileNotFoundError(f"no .csv file in {d}")
    raise ValueError(f"{d} holds {len(csvs)} .csv files "
                     f"({', '.join(p.name for p in csvs)}); expected one")


def crash_log_dir() -> Path:
    """Where crash and diagnostics logs go; see CRASH_LOG_DIR."""
    override = os.environ.get("SITREP91_LOG_DIR")
    return Path(override) if override else HOME_DIR / CRASH_LOG_DIR


def session_log_path(runs_dir: str | Path, started: datetime,
                     name: str) -> Path:
    """Where a CLI session's log goes: runs\\logs\\<date>_<time>_<name>.log."""
    safe = _UNSAFE.sub("-", name).strip("-") or "session"
    return (Path(runs_dir) / SESSION_LOG_DIR
            / f"{started:%Y-%m-%d_%H%M%S}_{safe}.log")
